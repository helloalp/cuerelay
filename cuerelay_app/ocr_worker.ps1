# Windows 自带 OCR（Windows.Media.Ocr）的调用外壳。
#
# 两种模式：
#   -Serve              常驻模式：从 stdin 逐行读请求，识别完输出 --END--
#   -Image <png> -Lang  单次模式：调试时用，识别完就退出
#
# 请求格式（常驻模式）：<语言标签>|<png 路径>
# 输出格式：每条识别结果一行，字段用 | 分隔：x|y|width|height|text
# 结束时输出 --END--；引擎不可用时输出 NO_ENGINE 后仍输出 --END--。

param(
    [string]$Image = "",
    [string]$Lang = "zh-Hans-CN",
    [switch]$Serve
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Storage.StorageFile, Windows.Foundation, ContentType = WindowsRuntime]

$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
})[0]

function Await($WinRtTask, $ResultType) {
    $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
    $netTask = $asTask.Invoke($null, @($WinRtTask))
    $netTask.Wait(-1) | Out-Null
    $netTask.Result
}

$engines = @{}

function Get-Engine($tag) {
    if ($engines.ContainsKey($tag)) { return $engines[$tag] }
    $engine = $null
    try {
        $language = [Windows.Globalization.Language]::new($tag)
        if ([Windows.Media.Ocr.OcrEngine]::IsLanguageSupported($language)) {
            $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
        }
    } catch {
        $engine = $null
    }
    if ($null -eq $engine) {
        $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
    }
    $engines[$tag] = $engine
    return $engine
}

function Invoke-Ocr($path, $tag) {
    $engine = Get-Engine $tag
    if ($null -eq $engine) {
        Write-Output "NO_ENGINE"
        return
    }
    $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($path)) ([Windows.Storage.StorageFile])
    $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    try {
        $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
        $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
        foreach ($line in $result.Lines) {
            $x1 = [double]::MaxValue
            $y1 = [double]::MaxValue
            $x2 = 0.0
            $y2 = 0.0
            foreach ($word in $line.Words) {
                $rect = $word.BoundingRect
                if ($rect.X -lt $x1) { $x1 = $rect.X }
                if ($rect.Y -lt $y1) { $y1 = $rect.Y }
                if (($rect.X + $rect.Width) -gt $x2) { $x2 = $rect.X + $rect.Width }
                if (($rect.Y + $rect.Height) -gt $y2) { $y2 = $rect.Y + $rect.Height }
            }
            $text = $line.Text -replace "\|", "/" -replace "[\r\n]", " "
            Write-Output ("{0}|{1}|{2}|{3}|{4}" -f [int]$x1, [int]$y1, [int]($x2 - $x1), [int]($y2 - $y1), $text)
        }
    } finally {
        $stream.Dispose()
    }
}

if ($Serve) {
    while ($true) {
        $request = [Console]::In.ReadLine()
        if ($null -eq $request) { break }
        $request = $request.Trim()
        if ($request -eq "") { continue }
        if ($request -eq "QUIT") { break }
        $parts = $request.Split("|")
        $tag = if ($parts.Count -ge 1 -and $parts[0]) { $parts[0] } else { "zh-Hans-CN" }
        $path = $parts[1]
        try {
            Invoke-Ocr $path $tag
        } catch {
            Write-Output ("ERROR " + $_.Exception.Message.Replace("`n", " "))
        }
        Write-Output "--END--"
        [Console]::Out.Flush()
    }
    exit 0
}

if ($Image -eq "") {
    Write-Output "NO_IMAGE"
    exit 3
}
try {
    Invoke-Ocr $Image $Lang
} catch {
    Write-Output ("ERROR " + $_.Exception.Message.Replace("`n", " "))
}
