# Generates a 100% Windows Shell-compliant multi-resolution ICO file
# 16, 24, 32, 48, 64, 128 px use standard Windows DIB format (uncompressed 32-bit BGRA)
# 256 px uses PNG format as per Windows Vista/7/10/11 Shell specification.

Add-Type -AssemblyName System.Drawing

function Draw-IconBitmap([int]$size) {
    $bmp = New-Object System.Drawing.Bitmap($size, $size, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $g.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
    $g.Clear([System.Drawing.Color]::Transparent)

    $scale = [double]$size / 512.0

    # Draw rounded squircle background
    $rectX = [float](16.0 * $scale)
    $rectY = [float](16.0 * $scale)
    $rectW = [float](480.0 * $scale)
    $rectH = [float](480.0 * $scale)
    $radius = [float](112.0 * $scale)

    $path = New-Object System.Drawing.Drawing2D.GraphicsPath
    $diam = $radius * 2.0
    $path.AddArc($rectX, $rectY, $diam, $diam, 180, 90)
    $path.AddArc($rectX + $rectW - $diam, $rectY, $diam, $diam, 270, 90)
    $path.AddArc($rectX + $rectW - $diam, $rectY + $rectH - $diam, $diam, $diam, 0, 90)
    $path.AddArc($rectX, $rectY + $rectH - $diam, $diam, $diam, 90, 90)
    $path.CloseFigure()

    # Background gradient
    $bgBrush = New-Object System.Drawing.Drawing2D.LinearGradientBrush(
        (New-Object System.Drawing.PointF(0, 0)),
        (New-Object System.Drawing.PointF(0, [float]$size)),
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

    $g.DrawLine($lPen, [float](148.0 * $scale), [float](136.0 * $scale), [float](148.0 * $scale), [float](376.0 * $scale))
    $g.DrawLine($lPen, [float](148.0 * $scale), [float](376.0 * $scale), [float](364.0 * $scale), [float](376.0 * $scale))

    # Inner 'E' Kinetic Streaming Rails (Cyan/Blue: #3B82F6)
    $ePen = New-Object System.Drawing.Pen([System.Drawing.Color]::FromArgb(255, 59, 130, 246), [Math]::Max(2.0, 26.0 * $scale))
    $ePen.StartCap = [System.Drawing.Drawing2D.LineCap]::Round
    $ePen.EndCap = [System.Drawing.Drawing2D.LineCap]::Round
    $ePen.LineJoin = [System.Drawing.Drawing2D.LineJoin]::Round

    # Vertical spine
    $g.DrawLine($ePen, [float](208.0 * $scale), [float](196.0 * $scale), [float](208.0 * $scale), [float](316.0 * $scale))
    # Top rail
    $g.DrawLine($ePen, [float](208.0 * $scale), [float](196.0 * $scale), [float](340.0 * $scale), [float](196.0 * $scale))
    # Middle dynamic rail
    $g.DrawLine($ePen, [float](208.0 * $scale), [float](256.0 * $scale), [float](304.0 * $scale), [float](256.0 * $scale))
    # Bottom rail
    $g.DrawLine($ePen, [float](208.0 * $scale), [float](316.0 * $scale), [float](348.0 * $scale), [float](316.0 * $scale))

    # Cryptographic Integrity Pip (Emerald Green: #10B981)
    $pipRadius = [Math]::Max(1.5, 15.0 * $scale)
    $pipBrush = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(255, 16, 185, 129))
    $pipX = [float]((356.0 * $scale) - $pipRadius)
    $pipY = [float]((136.0 * $scale) - $pipRadius)
    $g.FillEllipse($pipBrush, $pipX, $pipY, [float]($pipRadius * 2.0), [float]($pipRadius * 2.0))

    $g.Dispose()
    return $bmp
}

function Get-DIBFrameData([System.Drawing.Bitmap]$bmp) {
    $w = $bmp.Width
    $h = $bmp.Height
    
    $rect = New-Object System.Drawing.Rectangle(0, 0, $w, $h)
    $bmpData = $bmp.LockBits($rect, [System.Drawing.Imaging.ImageLockMode]::ReadOnly, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    
    $stride = [Math]::Abs($bmpData.Stride)
    $totalPixelsBytes = $stride * $h
    $rawPixels = New-Object byte[] $totalPixelsBytes
    [System.Runtime.InteropServices.Marshal]::Copy($bmpData.Scan0, $rawPixels, 0, $totalPixelsBytes)
    $bmp.UnlockBits($bmpData)

    # Invert rows to bottom-up (DIB requirement)
    $invertedPixels = New-Object byte[] $totalPixelsBytes
    for ($row = 0; $row -lt $h; $row++) {
        $srcOffset = $row * $stride
        $dstOffset = ($h - 1 - $row) * $stride
        [Array]::Copy($rawPixels, $srcOffset, $invertedPixels, $dstOffset, $stride)
    }

    # AND mask (1 bit per pixel, padded to 4 bytes per row)
    $andRowBytes = [int][Math]::Ceiling($w / 32.0) * 4
    $andMask = New-Object byte[] ($andRowBytes * $h)
    # Check pixels for complete transparency (alpha < 10) to set 1 in AND mask
    for ($row = 0; $row -lt $h; $row++) {
        $invRow = $h - 1 - $row
        $rowSrc = $invRow * $stride
        $rowDst = $row * $andRowBytes
        for ($col = 0; $col -lt $w; $col++) {
            $alpha = $rawPixels[$rowSrc + ($col * 4) + 3]
            if ($alpha -lt 10) {
                $byteIdx = [Math]::Floor($col / 8)
                $bitIdx = 7 - ($col % 8)
                $andMask[$rowDst + $byteIdx] = $andMask[$rowDst + $byteIdx] -bor (1 -shl $bitIdx)
            }
        }
    }

    # BITMAPINFOHEADER (40 bytes)
    $ms = New-Object System.IO.MemoryStream
    $bw = New-Object System.IO.BinaryWriter($ms)
    $bw.Write([uint32]40)              # biSize
    $bw.Write([int32]$w)               # biWidth
    $bw.Write([int32]($h * 2))         # biHeight (XOR + AND)
    $bw.Write([uint16]1)               # biPlanes
    $bw.Write([uint16]32)              # biBitCount
    $bw.Write([uint32]0)               # biCompression = BI_RGB
    $bw.Write([uint32]$totalPixelsBytes)# biSizeImage
    $bw.Write([int32]0)                # biXPelsPerMeter
    $bw.Write([int32]0)                # biYPelsPerMeter
    $bw.Write([uint32]0)               # biClrUsed
    $bw.Write([uint32]0)               # biClrImportant
    
    $bw.Write($invertedPixels)
    $bw.Write($andMask)
    $bw.Flush()
    
    $data = [byte[]]$ms.ToArray()
    $bw.Dispose()
    $ms.Dispose()
    return ,$data
}

$assetsDir = Join-Path $PSScriptRoot "..\assets"
if (!(Test-Path $assetsDir)) { New-Item -ItemType Directory -Path $assetsDir -Force }

# 1. Master PNG (512x512)
$bmp512 = Draw-IconBitmap 512
$masterPngPath = Join-Path $assetsDir "icon.png"
$bmp512.Save($masterPngPath, [System.Drawing.Imaging.ImageFormat]::Png)
$bmp512.Dispose()
Write-Host "Generated $masterPngPath (512x512 Master PNG)"

# 2. Multi-resolution ICO:
# Standard Windows sizes: 16, 24, 32, 48, 64, 128 as DIB; 256 as PNG
$sizes = @(16, 24, 32, 48, 64, 128, 256)
$iconFrames = @()

foreach ($sz in $sizes) {
    $bmp = Draw-IconBitmap $sz
    if ($sz -eq 256) {
        # 256 uses PNG frame
        $ms = New-Object System.IO.MemoryStream
        $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
        $frameBytes = [byte[]]$ms.ToArray()
        $ms.Dispose()
    } else {
        # 16 through 128 use true Windows DIB frame
        $frameBytes = [byte[]](Get-DIBFrameData $bmp)
    }
    $iconFrames += ,@($sz, $frameBytes)
    $bmp.Dispose()
}

$icoPath = Join-Path $assetsDir "icon.ico"
$fs = [System.IO.File]::Create($icoPath)
$bw = New-Object System.IO.BinaryWriter($fs)

# ICONDIR header
$bw.Write([uint16]0)                  # Reserved
$bw.Write([uint16]1)                  # Type 1 = Icon
$bw.Write([uint16]$iconFrames.Length) # Count

$offset = 6 + (16 * $iconFrames.Length)

# Directory entries
foreach ($f in $iconFrames) {
    $sz = $f[0]
    [byte[]]$data = $f[1]
    
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
foreach ($f in $iconFrames) {
    [byte[]]$data = $f[1]
    $fs.Write($data, 0, $data.Length)
}

$fs.Flush()
$bw.Dispose()
$fs.Dispose()

Write-Host "Generated native Windows Shell-compliant $icoPath (DIB 16-128 + PNG 256)"
