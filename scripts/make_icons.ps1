# Regenerates the app icons (bell on indigo gradient) using System.Drawing.
# Output: icon-512.png, icon-192.png, apple-touch-icon.png (180x180) in -OutDir.
# Icons are full-bleed squares (safe for PWA "maskable" purpose and iOS, which
# must not receive transparency). The bell geometry matches icon.svg in
# notify_pages.py. After regenerating, re-embed via scripts/embed_icons.py.
param([string]$OutDir = ".")

Add-Type -AssemblyName System.Drawing

function New-Icon([int]$Size) {
    $s = 512.0
    $bmp = New-Object System.Drawing.Bitmap(512, 512)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias

    # Full-bleed gradient background
    $c1 = [System.Drawing.ColorTranslator]::FromHtml('#6366F1')
    $c2 = [System.Drawing.ColorTranslator]::FromHtml('#4338CA')
    $grad = New-Object System.Drawing.Drawing2D.LinearGradientBrush(
        (New-Object System.Drawing.Point(0, 0)),
        (New-Object System.Drawing.Point(512, 512)), $c1, $c2)
    $g.FillRectangle($grad, 0, 0, 512, 512)

    $white = [System.Drawing.Brushes]::White

    # Top knob
    $g.FillEllipse($white, 238, 106, 36, 36)

    # Bell body (same path as icon.svg)
    $bell = New-Object System.Drawing.Drawing2D.GraphicsPath
    $bell.AddBezier(256, 140, 190, 140, 152, 186, 146, 246)
    $bell.AddLine(146, 246, 137, 330)
    $bell.AddLine(137, 330, 375, 330)
    $bell.AddLine(375, 330, 366, 246)
    $bell.AddBezier(366, 246, 360, 186, 322, 140, 256, 140)
    $bell.CloseFigure()
    $g.FillPath($white, $bell)

    # Rim (rounded bar)
    $rim = New-Object System.Drawing.Drawing2D.GraphicsPath
    $rim.AddArc(122, 330, 28, 28, 90, 180)
    $rim.AddArc(362, 330, 28, 28, 270, 180)
    $rim.CloseFigure()
    $g.FillPath($white, $rim)

    # Clapper
    $g.FillEllipse($white, 232, 366, 48, 48)

    $g.Dispose()

    # Flatten onto a fully opaque canvas at the target size. Downscaling with
    # DrawImage otherwise bleeds the border pixels toward transparency, and iOS
    # renders any transparency in the apple-touch-icon as black.
    $out = New-Object System.Drawing.Bitmap($Size, $Size, [System.Drawing.Imaging.PixelFormat]::Format24bppRgb)
    $go = [System.Drawing.Graphics]::FromImage($out)
    $go.Clear($c1)
    $go.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $go.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $go.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
    $attr = New-Object System.Drawing.Imaging.ImageAttributes
    $attr.SetWrapMode([System.Drawing.Drawing2D.WrapMode]::TileFlipXY)
    $dest = New-Object System.Drawing.Rectangle(0, 0, $Size, $Size)
    $go.DrawImage($bmp, $dest, 0, 0, 512, 512, [System.Drawing.GraphicsUnit]::Pixel, $attr)
    $go.Dispose()
    $bmp.Dispose()
    return $out
}

foreach ($spec in @(@(512, 'icon-512.png'), @(192, 'icon-192.png'), @(180, 'apple-touch-icon.png'))) {
    $img = New-Icon $spec[0]
    $path = Join-Path $OutDir $spec[1]
    $img.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)
    $img.Dispose()
    Write-Host "wrote $path"
}
