param([Parameter(Mandatory=$true)][string]$ImagePath, [string]$Language='zh-Hans-CN')
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Globalization, ContentType=WindowsRuntime]
$asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
} | Select-Object -First 1
function Await($Operation, $ResultType) {
    $task = $asTask.MakeGenericMethod($ResultType).Invoke($null, @($Operation))
    $task.GetAwaiter().GetResult()
}
# 直接写 UTF-8 字节到 stdout：PowerShell 5.1 默认按系统 code page 输出，
# 中文会被写成替换字符，导致后面「含不含中文」的判断全部失真。
function Out-Utf8($text) {
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($text)
    $stdout = [Console]::OpenStandardOutput()
    $stdout.Write($bytes, 0, $bytes.Length)
    $stdout.Flush()
}
$bitmap = $null
$ms = $null
$ras = $null
try {
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new($Language))
    if ($null -eq $engine) { throw 'Chinese OCR language is not installed' }
    # 关键：不能用 StorageFile.GetFileFromPathAsync —— 它受应用沙箱限制，
    # 读 data 目录（C:\AohuangDesktop\userdata\images\ocr）会抛「拒绝访问」，
    # 于是每一张图都变成「本地 OCR 未完成」，整批商品被判无法分类。
    # 改用 ReadAllBytes + AsRandomAccessStream，走普通文件 IO，不受沙箱限制。
    $bytes = [System.IO.File]::ReadAllBytes($ImagePath)
    $ms = New-Object System.IO.MemoryStream
    $ms.Write($bytes, 0, $bytes.Length)
    $ms.Position = 0
    $ras = [System.IO.WindowsRuntimeStreamExtensions]::AsRandomAccessStream($ms)
    $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($ras)) ([Windows.Graphics.Imaging.BitmapDecoder])
    if ([Math]::Max($decoder.PixelWidth, $decoder.PixelHeight) -gt [Windows.Media.Ocr.OcrEngine]::MaxImageDimension) {
        throw 'Image exceeds the local OCR size limit; visual review required'
    }
    $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
    $words = @($result.Lines | ForEach-Object {
        $_.Words | ForEach-Object {
            $bounds = $_.BoundingRect
            @{text=$_.Text; x=$bounds.X; y=$bounds.Y; width=$bounds.Width; height=$bounds.Height}
        }
    })
    $json = @{text=$result.Text; width=$decoder.PixelWidth; height=$decoder.PixelHeight; words=$words} | ConvertTo-Json -Depth 4 -Compress
    Out-Utf8 $json
} catch {
    $json = @{error='Local OCR failed; visual review required'; detail=$_.Exception.Message; language=$Language} | ConvertTo-Json -Compress
    Out-Utf8 $json
    exit 1
} finally {
    if ($null -ne $bitmap) { $bitmap.Dispose() }
    if ($null -ne $ras) { $ras.Dispose() }
    if ($null -ne $ms) { $ms.Dispose() }
}
