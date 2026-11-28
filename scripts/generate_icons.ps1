# Generates pixel-perfect multi-resolution Windows ICO and high-res PNG for LiveExtract
# Uses .NET System.Drawing with anti-aliasing and high-quality bicubic rendering.

Add-Type -AssemblyName System.Drawing

function Draw-IconFrame([int]$size) {
    $bmp = New-Object System.Drawing.Bitmap($size, $size, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $g.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
    $g.Clear([System.Drawing.Color]::Transparent)

    $scale = $size / 512.0

    # Draw rounded squircle background
    $rectX = 16.0 * $scale
    $rectY = 16.0 * $scale
    $rectW = 480.0 * $scale
    $rectH = 480.0 * $scale
    $radius = 112.0 * $scale

    $path = New-Object System.Drawing.Drawing2D.GraphicsPath
    $diam = $radius * 2
    $path.AddArc($rectX, $rectY, $diam, $diam, 180, 90)
    $path.AddArc($rectX + $rectW - $diam, $rectY, $diam, $diam, 270, 90)
    $path.AddArc($rectX + $rectW - $diam, $rectY + $rectH - $diam, $diam, $diam, 0, 90)
    $path.AddArc($rectX, $rectY + $rectH - $diam, $diam, $diam, 90, 90)
    $path.CloseFigure()

    # Background gradient
    $bgBrush = New-Object System.Drawing.Drawing2D.LinearGradientBrush(
        (New-Object System.Drawing.PointF(0, 0)),
        (New-Object System.Drawing.PointF(0, $size)),
        [System.Drawing.Color]::FromArgb(255, 26, 28, 35),
        [System.Drawing.Color]::FromArgb(255, 17, 18, 22)
    )
    $g.FillPath($bgBrush, $path)

    # Border
    $borderPen = New-Object System.Drawing.Pen([System.Drawing.Color]::FromArgb(255, 43, 47, 58), [Math]::Max(1.0, 3.0 * $scale))
    $g.DrawPath($borderPen, $path)

    # Architectural 'L' Base Frame (Refined Steel Slate: #4B5565)
    $lPen = New-Object System.Drawing.Pen([System.Drawing.Color]::FromArgb(255, 75, 85, 101), [Math]::Max(2.0, 28.0 * $scale))
    $lPen.StartCap = [System.Drawing.Drawing2D.LineCap]::Round
    $lPen.EndCap = [System.Drawing.Drawing2D.LineCap]::Round
    $lPen.LineJoin = [System.Drawing.Drawing2D.LineJoin]::Round

    $g.DrawLine($lPen, 148.0 * $scale, 136.0 * $scale, 148.0 * $scale, 376.0 * $scale)
    $g.DrawLine($lPen, 148.0 * $scale, 376.0 * $scale, 364.0 * $scale, 376.0 * $scale)

    # Inner 'E' Kinetic Streaming Rails (Cyan/Blue Gradient: #3B82F6 -> #06B6D4)
    $ePen = New-Object System.Drawing.Pen([System.Drawing.Color]::FromArgb(255, 59, 130, 246), [Math]::Max(2.0, 26.0 * $scale))
    $ePen.StartCap = [System.Drawing.Drawing2D.LineCap]::Round
    $ePen.EndCap = [System.Drawing.Drawing2D.LineCap]::Round
    $ePen.LineJoin = [System.Drawing.Drawing2D.LineJoin]::Round

    # Vertical spine
    $g.DrawLine($ePen, 208.0 * $scale, 196.0 * $scale, 208.0 * $scale, 316.0 * $scale)
    # Top rail
    $g.DrawLine($ePen, 208.0 * $scale, 196.0 * $scale, 340.0 * $scale, 196.0 * $scale)
    # Middle dynamic rail
    $g.DrawLine($ePen, 208.0 * $scale, 256.0 * $scale, 304.0 * $scale, 256.0 * $scale)
    # Bottom rail
    $g.DrawLine($ePen, 208.0 * $scale, 316.0 * $scale, 348.0 * $scale, 316.0 * $scale)

    # Cryptographic Integrity Pip (Emerald Green: #10B981)
    $pipRadius = [Math]::Max(1.5, 15.0 * $scale)
    $pipBrush = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(255, 16, 185, 129))
    $pipX = (356.0 * $scale) - $pipRadius
    $pipY = (136.0 * $scale) - $pipRadius
    $g.FillEllipse($pipBrush, $pipX, $pipY, $pipRadius * 2, $pipRadius * 2)

    $g.Dispose()
    return $bmp
}

$assetsDir = Join-Path $PSScriptRoot "..\assets"
if (!(Test-Path $assetsDir)) { New-Item -ItemType Directory -Path $assetsDir -Force }

# 1. Generate master 512x512 PNG
$masterBmp = Draw-IconFrame 512
$masterPngPath = Join-Path $assetsDir "icon.png"
$masterBmp.Save($masterPngPath, [System.Drawing.Imaging.ImageFormat]::Png)
$masterBmp.Dispose()
Write-Host "Generated $masterPngPath (512x512)"

# 2. Build multi-resolution ICO (256, 128, 64, 48, 32, 24, 16)
$sizes = @(256, 128, 64, 48, 32, 24, 16)
$pngFrames = @()

foreach ($sz in $sizes) {
    $frameBmp = Draw-IconFrame $sz
    $ms = New-Object System.IO.MemoryStream
    $frameBmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
    $pngFrames += ,@($sz, $ms.ToArray())
    $frameBmp.Dispose()
    $ms.Dispose()
}

$icoPath = Join-Path $assetsDir "icon.ico"
$fs = [System.IO.File]::Create($icoPath)
$bw = New-Object System.IO.BinaryWriter($fs)

# ICONDIR header
$bw.Write([uint16]0)          # Reserved
$bw.Write([uint16]1)          # Type 1 = Icon
$bw.Write([uint16]$sizes.Length) # Count

$offset = 6 + (16 * $sizes.Length)

# Directory entries
foreach ($f in $pngFrames) {
    $sz = $f[0]
    $data = $f[1]
    
    $bw.Write([byte]$(if ($sz -ge 256) { 0 } else { $sz })) # Width
    $bw.Write([byte]$(if ($sz -ge 256) { 0 } else { $sz })) # Height
    $bw.Write([byte]0)          # Colors
    $bw.Write([byte]0)          # Reserved
    $bw.Write([uint16]1)        # Planes
    $bw.Write([uint16]32)       # Bits per pixel
    $bw.Write([uint32]$data.Length) # Size
    $bw.Write([uint32]$offset)  # Offset
    
    $offset += $data.Length
}

# Image data
foreach ($f in $pngFrames) {
    $bw.Write($f[1])
}

$bw.Flush()
$bw.Dispose()
$fs.Dispose()

Write-Host "Generated multi-resolution $icoPath (Contains 16, 24, 32, 48, 64, 128, 256 px)"
