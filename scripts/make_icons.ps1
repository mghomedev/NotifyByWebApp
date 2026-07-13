# Regenerates the app icons using System.Drawing. The mark is a white bell with
# an amber wireless/broadcast signal (top-right) on an indigo gradient — a bell
# = notifications, the signal = "sent over the web / Web Push", so the icon is
# distinctive and identifies the service (not a generic bell). Geometry matches
# ICON_SVG in notify_pages.py.
#
# Output in -OutDir:
#   icon-512.png, icon-192.png, apple-touch-icon.png (180) — full-bleed OPAQUE
#     squares (safe for the PWA "maskable" purpose and iOS, which must not get
#     transparency).
#   badge.png (96) — a MONOCHROME, TRANSPARENT silhouette (white glyph, alpha
#     only). Android masks the notification small icon to its alpha channel, so a
#     full-colour opaque icon shows as a plain white square in the status bar;
#     this badge gives the real bell+signal silhouette instead.
# After regenerating, re-embed via scripts/embed_icons.py.
param([string]$OutDir = ".")

Add-Type -AssemblyName System.Drawing

function Draw-Glyph($g, $bellBrush, $accentBrush) {
    # Bell (knob + body + rim + clapper), shifted left so the top-right corner is
    # free for the broadcast signal.
    $g.FillEllipse($bellBrush, 214, 132, 34, 34)   # knob, center ~(231,149)

    $bell = New-Object System.Drawing.Drawing2D.GraphicsPath
    $bell.AddBezier(231, 150, 172, 150, 136, 196, 131, 252)
    $bell.AddLine(131, 252, 123, 330)
    $bell.AddLine(123, 330, 339, 330)
    $bell.AddLine(339, 330, 331, 252)
    $bell.AddBezier(331, 252, 326, 196, 290, 150, 231, 150)
    $bell.CloseFigure()
    $g.FillPath($bellBrush, $bell)

    $rim = New-Object System.Drawing.Drawing2D.GraphicsPath
    $rim.AddArc(108, 330, 28, 28, 90, 180)
    $rim.AddArc(326, 330, 28, 28, 270, 180)
    $rim.CloseFigure()
    $g.FillPath($bellBrush, $rim)

    $g.FillEllipse($bellBrush, 207, 366, 44, 44)   # clapper

    # Broadcast signal at the top-right: a source dot + two emanating arcs.
    $cx = 384.0; $cy = 150.0
    $g.FillEllipse($accentBrush, ($cx - 21), ($cy - 21), 42, 42)
    foreach ($r in @(48.0, 74.0)) {
        $pen = New-Object System.Drawing.Pen($accentBrush.Color, ($r * 0.30))
        $pen.StartCap = [System.Drawing.Drawing2D.LineCap]::Round
        $pen.EndCap = [System.Drawing.Drawing2D.LineCap]::Round
        $g.DrawArc($pen, ($cx - $r), ($cy - $r), (2 * $r), (2 * $r), -96.0, 150.0)
        $pen.Dispose()
    }
}

$C1 = [System.Drawing.ColorTranslator]::FromHtml('#6366F1')
$C2 = [System.Drawing.ColorTranslator]::FromHtml('#4338CA')
$AMBER = [System.Drawing.ColorTranslator]::FromHtml('#FBBF24')

function New-AppIcon([int]$Size) {
    $bmp = New-Object System.Drawing.Bitmap(512, 512)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $grad = New-Object System.Drawing.Drawing2D.LinearGradientBrush(
        (New-Object System.Drawing.Point(0, 0)),
        (New-Object System.Drawing.Point(512, 512)), $C1, $C2)
    $g.FillRectangle($grad, 0, 0, 512, 512)
    $white = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::White)
    $amber = New-Object System.Drawing.SolidBrush($AMBER)
    Draw-Glyph $g $white $amber
    $g.Dispose()

    # Flatten onto a fully opaque canvas at the target size (downscaling with
    # DrawImage otherwise bleeds the border toward transparency, which iOS
    # renders as black on the apple-touch-icon).
    $out = New-Object System.Drawing.Bitmap($Size, $Size, [System.Drawing.Imaging.PixelFormat]::Format24bppRgb)
    $go = [System.Drawing.Graphics]::FromImage($out)
    $go.Clear($C1)
    $go.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $go.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $go.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
    $attr = New-Object System.Drawing.Imaging.ImageAttributes
    $attr.SetWrapMode([System.Drawing.Drawing2D.WrapMode]::TileFlipXY)
    $dest = New-Object System.Drawing.Rectangle(0, 0, $Size, $Size)
    $go.DrawImage($bmp, $dest, 0, 0, 512, 512, [System.Drawing.GraphicsUnit]::Pixel, $attr)
    $go.Dispose(); $bmp.Dispose()
    return $out
}

function New-Badge([int]$Size) {
    $bmp = New-Object System.Drawing.Bitmap(512, 512, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $white = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::White)
    Draw-Glyph $g $white $white   # single-colour silhouette; Android masks to alpha
    $g.Dispose()
    $out = New-Object System.Drawing.Bitmap($Size, $Size, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $go = [System.Drawing.Graphics]::FromImage($out)
    $go.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $go.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $go.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
    $go.DrawImage($bmp, (New-Object System.Drawing.Rectangle(0, 0, $Size, $Size)), 0, 0, 512, 512, [System.Drawing.GraphicsUnit]::Pixel)
    $go.Dispose(); $bmp.Dispose()
    return $out
}

foreach ($spec in @(@(512, 'icon-512.png'), @(192, 'icon-192.png'), @(180, 'apple-touch-icon.png'))) {
    $img = New-AppIcon $spec[0]
    $path = Join-Path $OutDir $spec[1]
    $img.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)
    $img.Dispose()
    Write-Host "wrote $path"
}
$badge = New-Badge 96
$bpath = Join-Path $OutDir 'badge.png'
$badge.Save($bpath, [System.Drawing.Imaging.ImageFormat]::Png)
$badge.Dispose()
Write-Host "wrote $bpath"
