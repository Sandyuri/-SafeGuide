param(
    [string]$Root = (Split-Path -Parent $PSScriptRoot),
    [switch]$DrawEdges,
    [switch]$BusinessOnly,
    [switch]$StructureOnly,
    [switch]$TaskOnly,
    [switch]$LongEdgesOnly,
    [switch]$SplitEdgesOnly
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing

$mapPath = Join-Path $Root 'navigation_sandbox\maps\hospital_1f.navigation.json'
$anchorPath = Join-Path $Root 'navigation_sandbox\artifacts\floorplan\hospital_1f.floorplan_anchor_mapping.json'
$edgeRoutePath = Join-Path $Root 'navigation_sandbox\artifacts\floorplan\hospital_1f.edge_routes.json'
$floorplanPath = Join-Path $Root 'navigation_sandbox\artifacts\floorplan\source\hospital_1f_floorplan.jpg'
$outputName = if ($SplitEdgesOnly) {
    'hospital_1f.long_edge_split_verification.png'
} elseif ($LongEdgesOnly) {
    'hospital_1f.long_edges_over_8m_review.png'
} elseif ($TaskOnly) {
    'hospital_1f.medication_delivery_service_review.png'
} elseif ($BusinessOnly) {
    'hospital_1f.safegate_locations_review.png'
} elseif ($StructureOnly) {
    'hospital_1f.navigation_structure_review.png'
} elseif ($DrawEdges) {
    'hospital_1f.navigation_overlay_latest.png'
} else {
    'hospital_1f.all_nodes_overview.png'
}
$outputPath = Join-Path $Root "navigation_sandbox\artifacts\floorplan\$outputName"

$navigation = Get-Content -Raw -Encoding UTF8 $mapPath | ConvertFrom-Json
$anchors = Get-Content -Raw -Encoding UTF8 $anchorPath | ConvertFrom-Json
$edgeRoutes = Get-Content -Raw -Encoding UTF8 $edgeRoutePath | ConvertFrom-Json
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
                NodeClass = $node.node_class
                RoutingScope = $node.routing_scope
                GeneralNavigationAllowed = $node.general_navigation_allowed
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
if ($BusinessOnly) {
    $records = @($records | Where-Object NodeClass -eq 'safegate_location')
    if ($records.Count -ne 36) {
        throw "Expected 36 safegate_location nodes, found $($records.Count)."
    }
    # Keep colocated business nodes individually visible in the review image.
    # This changes display positions only; dashed leaders still point at the
    # unchanged route coordinate stored in the anchor mapping.
    $coordinateGroups = $records | Group-Object { "$($_.PixelX),$($_.PixelY)" } | Where-Object Count -gt 1
    foreach ($group in $coordinateGroups) {
        for ($i = 0; $i -lt $group.Count; $i++) {
            $offsetX = ($i - (($group.Count - 1) / 2.0)) * 180.0
            $group.Group[$i].DisplayX = $group.Group[$i].PixelX + $offsetX
            $group.Group[$i].DisplayY = $group.Group[$i].PixelY - 120.0
        }
    }
}
if ($StructureOnly) {
    $records = @($records | Where-Object NodeClass -eq 'navigation_structure')
    $expectedStructureCount = @($navigation.nodes | Where-Object node_class -eq 'navigation_structure').Count
    if ($records.Count -ne $expectedStructureCount) {
        throw "Expected $expectedStructureCount navigation_structure nodes, found $($records.Count)."
    }
}
if ($TaskOnly) {
    $taskEdges = @($navigation.edges | Where-Object routing_scope -eq 'medication_delivery_only')
    $taskNodeIds = @{}
    foreach ($edge in $taskEdges) {
        $taskNodeIds[$edge.from] = $true
        $taskNodeIds[$edge.to] = $true
    }
    $records = @($records | Where-Object { $taskNodeIds.ContainsKey($_.NodeId) })
    if ($taskEdges.Count -ne 8 -or $records.Count -ne 9) {
        throw "Expected eight medication-delivery edges and nine displayed endpoint nodes, found $($taskEdges.Count) edges / $($records.Count) nodes."
    }
}
if ($LongEdgesOnly) {
    $longEdges = @(
        $navigation.edges | Where-Object {
            $_.general_navigation_allowed -ne $false -and
            $_.routing_scope -ne 'medication_delivery_only' -and
            [double]$_.distance_m -gt 8.0
        } | Sort-Object { [double]$_.distance_m } -Descending
    )
    $longEdgeNodeIds = @{}
    foreach ($edge in $longEdges) {
        $longEdgeNodeIds[$edge.from] = $true
        $longEdgeNodeIds[$edge.to] = $true
    }
    $records = @($records | Where-Object { $longEdgeNodeIds.ContainsKey($_.NodeId) })
    if ($longEdges.Count -ne 11) {
        throw "Expected 11 general-navigation edges over 8 m, found $($longEdges.Count)."
    }
}
if ($SplitEdgesOnly) {
    $splitParentIds = @('E298', 'E287', 'E258', 'E296', 'E249', 'E292', 'E295', 'E269', 'E294', 'E267', 'E290')
    $splitEdges = @(
        $navigation.edges | Where-Object {
            $_.parent_edge_id -in $splitParentIds -and
            [int]$_.segment_count -eq 2 -and
            $_.general_navigation_allowed -ne $false -and
            $_.routing_scope -ne 'medication_delivery_only'
        } | Sort-Object parent_edge_id, segment_index
    )
    $splitEdgeNodeIds = @{}
    foreach ($edge in $splitEdges) {
        $splitEdgeNodeIds[$edge.from] = $true
        $splitEdgeNodeIds[$edge.to] = $true
    }
    $records = @($records | Where-Object { $splitEdgeNodeIds.ContainsKey($_.NodeId) })
    $insertedSplitNodes = @($records | Where-Object { $_.NodeId -like 'nav-f1-waypoint-0*' -and [int]$_.DisplayNumber -ge 129 })
    if ($splitEdges.Count -ne 22 -or $insertedSplitNodes.Count -ne 11) {
        throw "Expected 22 replacement edge segments and 11 inserted nodes, found $($splitEdges.Count) edges / $($insertedSplitNodes.Count) nodes."
    }
}

function Get-NodeColor([object]$record) {
    if ($record.NodeClass -eq 'navigation_structure') { return [System.Drawing.ColorTranslator]::FromHtml('#dc2626') }
    if ($record.NodeClass -in @('task_location', 'task_route_structure')) { return [System.Drawing.ColorTranslator]::FromHtml('#9333ea') }
    if ($record.NodeId -like 'nav-f1-safe-stop-*') { return [System.Drawing.ColorTranslator]::FromHtml('#16a34a') }
    if ($record.AccessZone -eq 'clinical_controlled') { return [System.Drawing.ColorTranslator]::FromHtml('#d97706') }
    return [System.Drawing.ColorTranslator]::FromHtml('#2563eb')
}

$canvasWidth = 7600
$canvasHeight = 3000
$mapLeft = 45
$mapTop = 190
$mapSize = 2650
$sidebarLeft = 2745
$rowsPerColumn = 30
$sidebarColumnWidth = 1200
$sourceLeft = if ($TaskOnly) { 1940.0 } else { 0.0 }
$sourceTop = if ($TaskOnly) { 2190.0 } else { 0.0 }
$sourceWidth = if ($TaskOnly) { 2450.0 } else { 6709.0 }
$sourceHeight = if ($TaskOnly) { 2450.0 } else { 6702.0 }

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
    $title = if ($SplitEdgesOnly) { 'RoboGuard Hospital 1F | 8 m Edge-split Verification' } elseif ($LongEdgesOnly) { 'RoboGuard Hospital 1F | Long-edge Review (> 8 m)' } elseif ($TaskOnly) { 'RoboGuard Hospital 1F | Medication Delivery Service Node' } elseif ($BusinessOnly) { 'RoboGuard Hospital 1F | 36 SafeGate Locations' } elseif ($StructureOnly) { "RoboGuard Hospital 1F | $($records.Count) Intermediate Navigation Nodes" } else { 'RoboGuard Hospital 1F | OpenUSD Point Navigation' }
    $graphics.DrawString($title, $titleFont, $darkBrush, 45, 38)
    $subtitle = if ($SplitEdgesOnly) {
        'The 11 former long edges are replaced by 22 green segments. New markers 129-139 are arc-length midpoint waypoints; every general edge is now <= 8 m.'
    } elseif ($LongEdgesOnly) {
        'Eleven general-navigation edges exceed 8 m. Red lines require deletion-or-split review; endpoint markers retain their map numbers.'
    } elseif ($TaskOnly) {
        'Medication-delivery-only route graph and its general-network entry nodes are shown. Purple edges are never exposed to general navigation.'
    } elseif ($BusinessOnly) {
        'Only node_class=safegate_location is shown. Yellow rings identify review targets; labels match the index.'
    } elseif ($StructureOnly) {
        'Only node_class=navigation_structure is shown. SafeGate and medication-delivery-only nodes are excluded.'
    } elseif ($DrawEdges) {
        "Current navigation JSON: $($records.Count) nodes and $($navigation.edges.Count) edges. Cyan is general navigation; purple dashed is delivery-only."
    } else {
        "Current navigation JSON: $($records.Count) nodes. Marker numbers match the index; edges are hidden for location review."
    }
    $graphics.DrawString($subtitle, $subtitleFont, $mutedBrush, 48, 95)

    $floorplan = [System.Drawing.Image]::FromFile($floorplanPath)
    try {
        if ($TaskOnly) {
            $graphics.DrawImage(
                $floorplan,
                [System.Drawing.RectangleF]::new($mapLeft, $mapTop, $mapSize, $mapSize),
                [System.Drawing.RectangleF]::new($sourceLeft, $sourceTop, $sourceWidth, $sourceHeight),
                [System.Drawing.GraphicsUnit]::Pixel
            )
        }
        else {
            $graphics.DrawImage($floorplan, $mapLeft, $mapTop, $mapSize, $mapSize)
        }
    }
    finally { $floorplan.Dispose() }
    $graphics.DrawRectangle($linePen, $mapLeft, $mapTop, $mapSize, $mapSize)

    if ($DrawEdges -or $TaskOnly -or $LongEdgesOnly -or $SplitEdgesOnly) {
        $anchorByNode = @{}
        foreach ($record in $records) { $anchorByNode[$record.NodeId] = $record }
        $generalEdgePen = New-Object System.Drawing.Pen ([System.Drawing.Color]::FromArgb(175, 8, 145, 178)), 4
        $generalEdgePen.LineJoin = [System.Drawing.Drawing2D.LineJoin]::Round
        $taskEdgePen = New-Object System.Drawing.Pen ([System.Drawing.Color]::FromArgb(220, 147, 51, 234)), 6
        $taskEdgePen.LineJoin = [System.Drawing.Drawing2D.LineJoin]::Round
        $taskEdgePen.DashStyle = [System.Drawing.Drawing2D.DashStyle]::Dash
        $longEdgePen = New-Object System.Drawing.Pen ([System.Drawing.Color]::FromArgb(235, 220, 38, 38)), 8
        $longEdgePen.LineJoin = [System.Drawing.Drawing2D.LineJoin]::Round
        $splitEdgePen = New-Object System.Drawing.Pen ([System.Drawing.Color]::FromArgb(235, 22, 163, 74)), 8
        $splitEdgePen.LineJoin = [System.Drawing.Drawing2D.LineJoin]::Round
        $labelBackBrush = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(235, 255, 255, 255))
        try {
            $edgesToDraw = if ($SplitEdgesOnly) { $splitEdges } elseif ($LongEdgesOnly) { $longEdges } elseif ($TaskOnly) { $taskEdges } else { $navigation.edges }
            foreach ($edge in $edgesToDraw) {
                $from = $anchorByNode[$edge.from]
                $to = $anchorByNode[$edge.to]
                if ($null -eq $from -or $null -eq $to) { throw "Edge $($edge.id) has no floorplan anchor." }
                $points = New-Object System.Collections.Generic.List[System.Drawing.PointF]
                $points.Add([System.Drawing.PointF]::new($mapLeft + (($from.PixelX - $sourceLeft) / $sourceWidth * $mapSize), $mapTop + (($from.PixelY - $sourceTop) / $sourceHeight * $mapSize)))
                $routeProperty = $edgeRoutes.edge_routes.PSObject.Properties[$edge.id]
                if ($null -ne $routeProperty) {
                    foreach ($waypoint in $routeProperty.Value) {
                        $points.Add([System.Drawing.PointF]::new($mapLeft + (([double]$waypoint[0] - $sourceLeft) / $sourceWidth * $mapSize), $mapTop + (([double]$waypoint[1] - $sourceTop) / $sourceHeight * $mapSize)))
                    }
                }
                $points.Add([System.Drawing.PointF]::new($mapLeft + (($to.PixelX - $sourceLeft) / $sourceWidth * $mapSize), $mapTop + (($to.PixelY - $sourceTop) / $sourceHeight * $mapSize)))
                $selectedEdgePen = if ($SplitEdgesOnly) { $splitEdgePen } elseif ($LongEdgesOnly) { $longEdgePen } elseif ($edge.routing_scope -eq 'medication_delivery_only') { $taskEdgePen } else { $generalEdgePen }
                if ($points.Count -gt 1) { $graphics.DrawLines($selectedEdgePen, $points.ToArray()) }
                $middle = $points[[math]::Floor(($points.Count - 1) / 2)]
                $edgeLabel = if ($edge.routing_scope -eq 'medication_delivery_only') {
                    "$($edge.id) [DELIVERY ONLY] $([double]$edge.distance_m)m"
                } else {
                    "$($edge.id)  $([double]$edge.distance_m)m"
                }
                $labelSize = $graphics.MeasureString($edgeLabel, $edgeFont)
                $labelRect = New-Object System.Drawing.RectangleF ($middle.X - $labelSize.Width / 2 - 3), ($middle.Y - $labelSize.Height / 2 - 2), ($labelSize.Width + 6), ($labelSize.Height + 4)
                $graphics.FillRectangle($labelBackBrush, $labelRect)
                $graphics.DrawString($edgeLabel, $edgeFont, $darkBrush, $labelRect.X + 3, $labelRect.Y + 2)
            }
        }
        finally { $generalEdgePen.Dispose(); $taskEdgePen.Dispose(); $longEdgePen.Dispose(); $splitEdgePen.Dispose(); $labelBackBrush.Dispose() }
    }

    $index = 1
    foreach ($record in $records) {
        $color = Get-NodeColor $record
        $brush = New-Object System.Drawing.SolidBrush $color
        $borderColor = if ($BusinessOnly -or $StructureOnly -or $TaskOnly -or $LongEdgesOnly -or $SplitEdgesOnly) { [System.Drawing.ColorTranslator]::FromHtml('#facc15') } else { [System.Drawing.Color]::White }
        $borderWidth = if ($BusinessOnly -or $StructureOnly -or $TaskOnly -or $LongEdgesOnly -or $SplitEdgesOnly) { 8 } else { 4 }
        $border = New-Object System.Drawing.Pen $borderColor, $borderWidth
        $x = $mapLeft + (($record.DisplayX - $sourceLeft) / $sourceWidth * $mapSize)
        $y = $mapTop + (($record.DisplayY - $sourceTop) / $sourceHeight * $mapSize)
        if ($record.DisplayX -ne $record.PixelX -or $record.DisplayY -ne $record.PixelY) {
            $routeX = $mapLeft + (($record.PixelX - $sourceLeft) / $sourceWidth * $mapSize)
            $routeY = $mapTop + (($record.PixelY - $sourceTop) / $sourceHeight * $mapSize)
            $leader = [System.Drawing.Pen]::new([System.Drawing.Color]::FromArgb(180, 71, 85, 105), [single]2)
            $leader.DashStyle = [System.Drawing.Drawing2D.DashStyle]::Dash
            $graphics.DrawLine($leader, [single]$routeX, [single]$routeY, [single]$x, [single]$y)
            $leader.Dispose()
        }
        $diameter = if ($BusinessOnly -or $StructureOnly -or $TaskOnly -or $LongEdgesOnly -or $SplitEdgesOnly) { 49 } else { 37 }
        $graphics.FillEllipse($brush, $x - $diameter / 2, $y - $diameter / 2, $diameter, $diameter)
        $graphics.DrawEllipse($border, $x - $diameter / 2, $y - $diameter / 2, $diameter, $diameter)
        $text = '{0:D2}' -f $record.DisplayNumber
        $size = $graphics.MeasureString($text, $bubbleFont)
        $graphics.DrawString($text, $bubbleFont, $whiteBrush, $x - $size.Width / 2, $y - $size.Height / 2 - 1)
        $brush.Dispose(); $border.Dispose()
        $index++
    }

    $sidebarTitle = if ($LongEdgesOnly) { 'Long-edge review index' } elseif ($SplitEdgesOnly) { 'Replacement-route node index' } else { 'Node index (marker number)' }
    $graphics.DrawString($sidebarTitle, $titleFont, $darkBrush, $sidebarLeft, 190)
    $categories = if ($SplitEdgesOnly) {
        @(
            @{ Color = '#16a34a'; Text = 'Replacement edge segment (<= 8 m)' },
            @{ Color = '#facc15'; Text = 'Endpoints and inserted midpoint nodes' }
        )
    } elseif ($LongEdgesOnly) {
        @(
            @{ Color = '#dc2626'; Text = 'General-navigation edge longer than 8 m' },
            @{ Color = '#facc15'; Text = 'Endpoint requiring review' }
        )
    } elseif ($TaskOnly) {
        @(
            @{ Color = '#9333ea'; Text = 'Medication-delivery-only node' },
            @{ Color = '#dc2626'; Text = 'General navigation connection point' }
        )
    } elseif ($BusinessOnly) {
        @(
            @{ Color = '#2563eb'; Text = 'SafeGate public location' },
            @{ Color = '#d97706'; Text = 'Controlled-area entrance' },
            @{ Color = '#16a34a'; Text = 'Safe stop' }
        )
    } elseif ($StructureOnly) {
        @(
            @{ Color = '#dc2626'; Text = 'Intermediate navigation node' }
        )
    } else {
        @(
            @{ Color = '#2563eb'; Text = 'SafeGate public location' },
            @{ Color = '#d97706'; Text = 'Controlled-area entrance' },
            @{ Color = '#dc2626'; Text = 'Navigation structure' },
            @{ Color = '#9333ea'; Text = 'Task-only location' },
            @{ Color = '#16a34a'; Text = 'Safe stop' }
        )
    }
    $legendX = $sidebarLeft
    foreach ($category in $categories) {
        $legendBrush = New-Object System.Drawing.SolidBrush ([System.Drawing.ColorTranslator]::FromHtml($category.Color))
        $graphics.FillEllipse($legendBrush, $legendX, 260, 19, 19)
        $graphics.DrawString($category.Text, $legendFont, $mutedBrush, $legendX + 28, 258)
        $legendBrush.Dispose()
        $legendX += 360
    }

    if ($LongEdgesOnly) {
        $recordById = @{}
        foreach ($record in $records) { $recordById[$record.NodeId] = $record }
        $index = 1
        foreach ($edge in $longEdges) {
            $column = [math]::Floor(($index - 1) / 6)
            $row = ($index - 1) % 6
            $x = $sidebarLeft + ($column * 2100)
            $y = 340 + ($row * 190)
            $fromRecord = $recordById[$edge.from]
            $toRecord = $recordById[$edge.to]
            $edgeBrush = New-Object System.Drawing.SolidBrush ([System.Drawing.ColorTranslator]::FromHtml('#dc2626'))
            $graphics.FillEllipse($edgeBrush, $x, $y + 2, 32, 32)
            $graphics.DrawString($index.ToString(), $numberFont, $whiteBrush, $x + 8, $y + 8)
            $graphics.DrawString("$($edge.id)   $([double]$edge.distance_m)m", $listFont, $darkBrush, $x + 48, $y)
            $graphics.DrawString("$($fromRecord.DisplayNumber) $($fromRecord.NameZh)  ->  $($toRecord.DisplayNumber) $($toRecord.NameZh)", $legendFont, $mutedBrush, $x + 48, $y + 46)
            $graphics.DrawString("$($edge.from)  ->  $($edge.to)", $legendFont, $mutedBrush, $x + 48, $y + 84)
            $edgeBrush.Dispose()
            $index++
        }
    }
    else {
        $index = 1
        foreach ($record in $records) {
            $column = [math]::Floor(($index - 1) / $rowsPerColumn)
            $row = ($index - 1) % $rowsPerColumn
            $x = $sidebarLeft + ($column * $sidebarColumnWidth)
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
    }

    $footer = if ($SplitEdgesOnly) { 'Verification image: green segments preserve their parent polylines; new midpoint positions still require floor-plan and on-site review.' } elseif ($LongEdgesOnly) { 'Review image: red edges exceed the proposed 8 m hard limit; inspect for redundancy, wall crossings, or required intermediate nodes.' } elseif ($TaskOnly) { 'Review image: purple dashed TDE edges and purple nodes are medication-delivery-only; never use for general navigation.' } elseif ($BusinessOnly) { "Review image: 36 SafeGate locations only; $(@($navigation.nodes | Where-Object node_class -eq 'navigation_structure').Count) navigation-structure nodes are hidden." } elseif ($StructureOnly) { "Review image: $($records.Count) intermediate navigation nodes only; SafeGate and task-only nodes are hidden." } elseif ($DrawEdges) { 'Point-robot route model; openable doors are treated as open and robot footprint is not included.' } else { 'Point-robot navigation nodes registered to the OpenUSD static-collision map.' }
    $graphics.DrawString('Positions: coordinate_mapping.json and floorplan_anchor_mapping.json; routes: edge_routes.json.', $legendFont, $mutedBrush, 48, 2910)
    $graphics.DrawString($footer, $legendFont, $mutedBrush, 2100, 2910)

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
